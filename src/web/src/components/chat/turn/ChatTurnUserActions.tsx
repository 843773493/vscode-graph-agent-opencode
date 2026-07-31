import React from "react";
import type { ConversationView } from "../../../types/frontend";
import { fileToSelectedAttachment } from "../../../utils/mediaAttachments";
import MessageAttachments from "../MessageAttachments";
import PendingRequestActions from "../PendingRequestActions";
import ProgressiveUserMessage from "../ProgressiveUserMessage";
import type {
  ChatTurnActionCallbacks,
  ChatTurnActions,
} from "./useChatTurnActions";

export function ChatTurnUserSection({
  apiPort,
  workspaceId,
  conversation,
  sessionBusy,
  actions,
  onRemovePending,
  onSendPendingImmediately,
  onChangePendingKind,
}: Pick<
  ChatTurnActionCallbacks,
  "onRemovePending" | "onSendPendingImmediately" | "onChangePendingKind"
> & {
  apiPort: number;
  workspaceId?: string | null;
  conversation: ConversationView;
  sessionBusy: boolean;
  actions: ChatTurnActions;
}): React.ReactNode {
  const editAttachmentInputRef = React.useRef<HTMLInputElement | null>(null);
  const userMessage = conversation.userMessage;
  if (!userMessage) return null;
  const summaryOnly = conversation.turnItemsView === "summary";
  const userAttachments = userMessage.attachments ?? [];

  return (
    <div className={`chat-user-row${actions.isInternalDisplayMessage ? " is-internal" : ""}`}>
      <div className={`chat-user-bubble${actions.editing ? " is-editing" : ""}${actions.isInternalDisplayMessage ? " is-internal" : ""}`}>
        {actions.editing ? (
          <form
            className="chat-request-edit-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (conversation.pending) void actions.executePendingEdit();
              else void actions.executeReplay("edit_and_continue", actions.editContent.trim());
            }}
          >
            <textarea
              className="chat-request-edit-input"
              value={actions.editContent}
              aria-label="编辑用户消息"
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
                      aria-label={`移除附件 ${attachment.name ?? attachment.file_id}`}
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
                  添加附件
                </button>
              </>
            ) : null}
            {!summaryOnly && !conversation.pending && !actions.isInternalDisplayMessage ? (
              <div className="chat-turn-action-warning">
                将移除此消息之后的会话上下文，但不会撤销已产生的文件修改。
              </div>
            ) : null}
            <div className="chat-request-edit-actions">
              <button
                type="button"
                disabled={actions.actionRunning}
                onClick={actions.cancelEditing}
              >
                取消
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
                  ? "正在保存..."
                  : conversation.pending
                    ? "保存"
                    : "编辑并从此处继续"}
              </button>
            </div>
          </form>
        ) : (
          <>
            {!summaryOnly && !conversation.pending && !actions.isInternalDisplayMessage ? (
              <button
                type="button"
                className="chat-request-edit-button"
                title="编辑并从此处继续"
                aria-label="编辑并从此处继续"
                disabled={sessionBusy}
                onClick={actions.startEditing}
              >
                <span className="codicon codicon-edit" aria-hidden="true" />
              </button>
            ) : null}
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
              />
            ) : null}
          </>
        )}
        {conversation.pending
          && !actions.isInternalDisplayMessage
          && conversation.pendingKind ? (
          <PendingRequestActions
            kind={conversation.pendingKind}
            disabled={actions.actionRunning}
            onEdit={actions.startEditing}
            onSendImmediately={() => {
              void actions.executePendingAction(
                () => onSendPendingImmediately(userMessage.message_id),
              );
            }}
            onRemove={() => {
              void actions.executePendingAction(
                () => onRemovePending(userMessage.message_id),
              );
            }}
            onChangeKind={(kind) => {
              void actions.executePendingAction(
                () => onChangePendingKind(userMessage.message_id, kind),
              );
            }}
          />
        ) : null}
      </div>
    </div>
  );
}
