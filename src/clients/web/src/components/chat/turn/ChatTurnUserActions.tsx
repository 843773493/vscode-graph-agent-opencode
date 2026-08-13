import React from "react";
import type { ConversationView } from "../../../types/frontend";
import { fileToSelectedAttachment } from "../../../utils/mediaAttachments";
import AnchoredOverlay from "../../AnchoredOverlay";
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
  onLoadAgentStateMessageRawContent,
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
  onLoadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
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
    && (canEditUserMessage || canInspectRawMessage),
  );
  const rawMessageAriaLabel = userMessage?.content.trim()
    ? "内部用户消息，右键展开原始消息"
    : "空用户消息，右键展开原始消息";
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
      setRawMessageError(error instanceof Error ? error.message : String(error));
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
        title={canInspectRawMessage ? "右键展开或收起原始消息详情" : undefined}
        onContextMenu={handleRawMessageContextMenu}
      >
        {canShowActionMenu ? (
          <div ref={actionMenuAnchorRef} className="chat-user-action-menu-anchor">
            <button
              type="button"
              className="chat-user-action-menu-trigger"
              title="消息操作"
              aria-label="消息操作"
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
              <div className="chat-user-action-menu" role="menu" aria-label="用户消息操作">
                {canEditUserMessage ? (
                  <button
                    type="button"
                    role="menuitem"
                    className="chat-user-action-menu-item"
                    title="编辑并从此处继续"
                    aria-label="编辑并从此处继续"
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
                    title={rawMessageContent !== null ? "收起隐藏内容" : "展示隐藏内容"}
                    aria-label={rawMessageContent !== null ? "收起隐藏内容" : "展示隐藏内容"}
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
        {canInspectRawMessage && rawMessageLoading ? (
          <div className="chat-user-raw-status" role="status">正在读取原始消息…</div>
        ) : null}
        {canInspectRawMessage && rawMessageError ? (
          <div className="chat-user-raw-error" role="alert">{rawMessageError}</div>
        ) : null}
        {canInspectRawMessage && rawMessageContent !== null ? (
          <div className="chat-user-raw-details">
            <div className="chat-user-raw-details-title">原始消息详情（标记文本）</div>
            <pre className="chat-user-raw-content">
              {rawMessageContent || "（原始消息为空）"}
            </pre>
          </div>
        ) : null}
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
