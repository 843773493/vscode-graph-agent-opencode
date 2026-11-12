import { t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import type { AttachmentRef, DeliveryPolicy } from "../../../types/backend";
import type { ConversationView } from "../../../types/frontend";
import { fileToSelectedAttachment } from "../../../utils/media/mediaAttachments";
import AnchoredOverlay from "../../overlays/AnchoredOverlay";
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
  const actionMenuAnchorRef = React.useRef<HTMLDivElement | null>(null);
  const userMessage = conversation.userMessage;
  const userMessageId = userMessage?.message_id ?? null;
  const [actionMenuOpen, setActionMenuOpen] = React.useState(false);
  const [rawMessageContent, setRawMessageContent] = React.useState<string | null>(null);
  const [rawMessageLoading, setRawMessageLoading] = React.useState(false);
  const [rawMessageError, setRawMessageError] = React.useState<string | null>(null);

  React.useEffect(() => {
    setActionMenuOpen(false);
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
  const canShowActionMenu = Boolean(
    userMessage
    && !actions.editing
    && (canEditUserMessage || canInspectRawMessage || (conversation.displayMode === "history" && conversation.turnId && onForkConversation)),
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
  const handleRawMessageContextMenu = React.useCallback((
    event: React.MouseEvent<HTMLDivElement>,
  ) => {
    if (!canInspectRawMessage) return;
    event.preventDefault();
    void toggleRawMessageDetails();
  }, [canInspectRawMessage, toggleRawMessageDetails]);
  const handleEditAction = React.useCallback(() => {
    setActionMenuOpen(false);
    actions.startEditing();
  }, [actions.startEditing]);
  const handleRawDetailsAction = React.useCallback(() => {
    setActionMenuOpen(false);
    void toggleRawMessageDetails();
  }, [toggleRawMessageDetails]);

  if (!userMessage) return null;
  const userAttachments = userMessage.attachments ?? [];

  return (
    <div className={`chat-user-row${actions.isInternalDisplayMessage ? " is-internal" : ""}`}>
      <div
        className={`chat-user-bubble${actions.editing ? " is-editing" : ""}${actions.isInternalDisplayMessage ? " is-internal" : ""}${canInspectRawMessage ? " is-inspectable" : ""}`}
        role={canInspectRawMessage ? "group" : undefined}
        aria-label={canInspectRawMessage ? rawMessageAriaLabel : undefined}
        title={canInspectRawMessage ? t("chat.turn.rawHint") : undefined}
        onContextMenu={handleRawMessageContextMenu}
      >
        {canShowActionMenu ? (
          <div ref={actionMenuAnchorRef} className="chat-user-action-menu-anchor">
            <button
              type="button"
              className="chat-user-action-menu-trigger"
              title={t("chat.turn.actions")}
              aria-label={t("chat.turn.actions")}
              aria-haspopup="menu"
              aria-expanded={actionMenuOpen}
              onClick={() => setActionMenuOpen((open) => !open)}
            >
              <span className="codicon codicon-add" aria-hidden="true" />
            </button>
            <AnchoredOverlay
              open={actionMenuOpen}
              anchorRef={actionMenuAnchorRef}
              placement="bottom-end"
              offset={4}
              onClose={() => setActionMenuOpen(false)}
            >
              <div className="chat-user-action-menu" role="menu" aria-label={t("chat.turn.userActions")}>
                {conversation.displayMode === "history" && conversation.turnId && onForkConversation ? (
                  <button type="button" role="menuitem" className="chat-user-action-menu-item"
                    title={t("chat.turn.fork")} aria-label={t("chat.turn.fork")}
                    onClick={() => {
                      if (!workspaceId || !conversation.threadId || !conversation.sourceViewId || conversation.sourceViewRevision === undefined) {
                        throw new Error("历史消息缺少实际读取的来源快照");
                      }
                      setActionMenuOpen(false);
                      onForkConversation({
                        workspaceId, sessionId: conversation.sessionId, threadId: conversation.threadId,
                        sourceViewId: conversation.sourceViewId, sourceViewRevision: conversation.sourceViewRevision,
                        turnId: conversation.turnId,
                      });
                    }}>
                    <span className="codicon codicon-git-pull-request" aria-hidden="true" />
                  </button>
                ) : null}
                {canEditUserMessage ? (
                  <button
                    type="button"
                    role="menuitem"
                    className="chat-user-action-menu-item"
                    title={t("chat.turn.continue")}
                    aria-label={t("chat.turn.continue")}
                    disabled={sessionBusy}
                    onClick={handleEditAction}
                  >
                    <span className="codicon codicon-edit" aria-hidden="true" />
                  </button>
                ) : null}
                {canInspectRawMessage ? (
                  <button
                    type="button"
                    role="menuitem"
                    className="chat-user-action-menu-item"
                    title={rawMessageContent !== null ? t("chat.turn.hideRaw") : t("chat.turn.showRaw")}
                    aria-label={rawMessageContent !== null ? t("chat.turn.hideRaw") : t("chat.turn.showRaw")}
                    onClick={handleRawDetailsAction}
                  >
                    <span className="codicon codicon-eye" aria-hidden="true" />
                  </button>
                ) : null}
              </div>
            </AnchoredOverlay>
          </div>
        ) : null}
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
          <div className="chat-user-raw-error" role="alert">{rawMessageError}</div>
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
