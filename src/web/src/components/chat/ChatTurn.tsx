import React from "react";
import { conversationTokenUsage } from "../../state/tokenUsage";
import { aggregateConversationEvents, buildPendingStatusItem } from "../../state/trace/traceAggregation";
import type { TimelineItem } from "../../state/timelineTypes";
import type { ConversationView } from "../../types/frontend";
import { fileToSelectedAttachment } from "../../utils/mediaAttachments";
import type {
  AttachmentRef,
  MessageReplayRequest,
} from "../../types/backend";
import MessageAttachments from "./MessageAttachments";
import MarkdownContent from "./MarkdownContent";
import ResponseActionToolbar from "./ResponseActionToolbar";
import ThinkingSection from "./ThinkingSection";
import ToolRow from "./ToolRow";
import PendingRequestActions from "./PendingRequestActions";

function assistantFallback(conversation: ConversationView): string {
  const messages = conversation.assistantMessages ?? [];
  const candidates = messages
    .map((message) => ({
      content: message.content.trim(),
      phase: message.metadata?.phase,
    }))
    .filter((candidate) => candidate.content.length > 0);
  const isFragmented = (content: string) => {
    const lines = content.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length >= 5 && content.length / lines.length < 6) {
      return true;
    }
    if (lines.length < 10) {
      return false;
    }
    const tinyLines = lines.filter((line) => line.length <= 2).length;
    return tinyLines / lines.length >= 0.35;
  };
  const healthyFinal = candidates.filter(
    (candidate) =>
      candidate.phase === "final_answer" && !isFragmented(candidate.content),
  );
  if (healthyFinal.length > 0) {
    return healthyFinal.reduce(
      (best, candidate) => candidate.content.length > best.length ? candidate.content : best,
      "",
    );
  }
  const healthy = candidates.filter((candidate) => !isFragmented(candidate.content));
  return healthy.reduce(
    (best, candidate) => candidate.content.length > best.length ? candidate.content : best,
    "",
  );
}

function ErrorPart({ item }: { item: Extract<TimelineItem, { kind: "trace" }> }) {
  const message = [item.payload.error, item.payload.message, item.payload.detail]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  return (
    <div className="chat-inline-error" role="alert">
      <span className="codicon codicon-error" aria-hidden="true" />
      <span>{message ?? "运行失败"}</span>
    </div>
  );
}

function ResponsePart({
  item,
  showRawDetails,
}: {
  item: TimelineItem;
  showRawDetails: boolean;
}): React.ReactNode {
  if (item.kind === "aggregated_text" && item.partKind === "markdown") {
    return (
      <MarkdownContent
        value={item.text}
        className={item.active ? "is-streaming" : ""}
      />
    );
  }
  if (item.kind === "aggregated_tool") {
    return <ToolRow item={item} showRawDetails={showRawDetails} />;
  }
  if (
    item.kind === "trace" &&
    ["error", "job_failed", "job_cancelled", "session_interrupted"].includes(item.eventType)
  ) {
    return <ErrorPart item={item} />;
  }
  return null;
}

type WorkItem =
  | Extract<TimelineItem, { kind: "aggregated_text" }>
  | Extract<TimelineItem, { kind: "aggregated_tool" }>;

type RenderGroup =
  | { kind: "work"; id: string; items: WorkItem[] }
  | { kind: "response"; id: string; item: TimelineItem };

function buildRenderGroups(items: TimelineItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  let finalMarkdownIndex = -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "aggregated_text" && item.partKind === "markdown") {
      finalMarkdownIndex = index;
      break;
    }
  }
  for (const [index, item] of items.entries()) {
    const isWork =
      item.kind === "aggregated_tool" ||
      (item.kind === "aggregated_text" &&
        (item.partKind === "reasoning" || index !== finalMarkdownIndex));
    if (!isWork) {
      groups.push({ kind: "response", id: item.id, item });
      continue;
    }
    const previous = groups[groups.length - 1];
    if (previous?.kind === "work") {
      previous.items.push(item);
    } else {
      groups.push({ kind: "work", id: `work-${item.id}`, items: [item] });
    }
  }
  return groups;
}

export default function ChatTurn({
  apiPort,
  workspaceId,
  conversation,
  showRawDetails,
  isLastTurn,
  sessionBusy,
  onReplayTurn,
  onUpdatePending,
  onRemovePending,
  onSendPendingImmediately,
  onChangePendingKind,
}: {
  apiPort: number;
  workspaceId?: string | null;
  conversation: ConversationView;
  showRawDetails: boolean;
  isLastTurn: boolean;
  sessionBusy: boolean;
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
}): React.ReactNode {
  const [editing, setEditing] = React.useState(false);
  const [editContent, setEditContent] = React.useState("");
  const [editAttachments, setEditAttachments] = React.useState<AttachmentRef[]>([]);
  const [confirmAction, setConfirmAction] = React.useState<
    "retry_failed" | "regenerate" | null
  >(null);
  const [actionRunning, setActionRunning] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const editAttachmentInputRef = React.useRef<HTMLInputElement | null>(null);
  const parts = React.useMemo(
    () => aggregateConversationEvents(
      conversation.events,
      conversation.conversationId,
      conversation.status === "running" || conversation.status === "queued",
    ),
    [conversation],
  );
  const running = conversation.status === "running" || conversation.status === "queued";
  const persistedResponse = assistantFallback(conversation);
  const preferPersistedResponse = !running && Boolean(persistedResponse);
  const visibleParts = parts.filter((item) =>
    (item.kind === "aggregated_text" &&
      (!preferPersistedResponse || item.partKind !== "markdown")) ||
    item.kind === "aggregated_tool" ||
    (item.kind === "trace" &&
      ["error", "job_failed", "job_cancelled", "session_interrupted"].includes(item.eventType)),
  );
  const renderGroups = buildRenderGroups(visibleParts);
  const finalTextPart = [...visibleParts].reverse().find(
    (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
      item.kind === "aggregated_text" && item.partKind === "markdown",
  );
  const hasFinalText = Boolean(finalTextPart);
  const fallback = preferPersistedResponse
    ? persistedResponse
    : hasFinalText
      ? ""
      : persistedResponse;
  const finalResponseText = finalTextPart?.text ?? fallback;
  const hasActiveWork = visibleParts.some(
    (item) =>
      (item.kind === "aggregated_tool" ||
        (item.kind === "aggregated_text" && item.partKind === "reasoning")) &&
      item.active,
  );
  const hasStreamingMarkdown = visibleParts.some(
    (item) => item.kind === "aggregated_text" && item.partKind === "markdown" && item.active,
  );
  const status = running && !hasActiveWork && !hasStreamingMarkdown
    ? buildPendingStatusItem(conversation)
    : null;
  const showResponseActions = !running && (hasFinalText || Boolean(fallback));
  const tokenUsage = React.useMemo(
    () => conversationTokenUsage(conversation),
    [conversation],
  );
  const userMessage = conversation.userMessage;
  const userAttachments = userMessage?.attachments ?? [];
  const failedByJob = isLastTurn
    && conversation.status === "error"
    && !conversation.events.some((event) =>
      ["job_cancelled", "session_interrupted"].includes(event.type),
    );

  const startEditing = React.useCallback(() => {
    if (!userMessage || (sessionBusy && !conversation.pending)) {
      return;
    }
    setEditContent(userMessage.content);
    setEditAttachments(userAttachments);
    setActionError(null);
    setConfirmAction(null);
    setEditing(true);
  }, [conversation.pending, sessionBusy, userAttachments, userMessage]);

  const executeReplay = React.useCallback(async (
    action: MessageReplayRequest["action"],
    content?: string,
  ) => {
    if (!userMessage || actionRunning || sessionBusy) {
      return;
    }
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
  }, [actionRunning, onReplayTurn, sessionBusy, userAttachments, userMessage]);

  const executePendingEdit = React.useCallback(async () => {
    if (!userMessage || actionRunning || !conversation.pending) {
      return;
    }
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
    editContent,
    onUpdatePending,
    editAttachments,
    userMessage,
  ]);

  const executePendingAction = React.useCallback(async (
    action: () => Promise<void>,
  ) => {
    if (actionRunning) {
      return;
    }
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

  return (
    <article className="chat-turn" data-conversation-id={conversation.conversationId}>
      {userMessage ? (
        <div className="chat-user-row">
          <div className={`chat-user-bubble${editing ? " is-editing" : ""}`}>
            {editing ? (
              <form
                className="chat-request-edit-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (conversation.pending) {
                    void executePendingEdit();
                  } else {
                    void executeReplay("edit_and_continue", editContent.trim());
                  }
                }}
              >
                <textarea
                  className="chat-request-edit-input"
                  value={editContent}
                  aria-label="编辑用户消息"
                  autoFocus
                  disabled={actionRunning}
                  onChange={(event) => setEditContent(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      setEditing(false);
                      setActionError(null);
                    }
                  }}
                />
                {editAttachments.length > 0 ? (
                  <div className="chat-request-edit-attachments">
                    {editAttachments.map((attachment) => (
                      <span key={attachment.file_id} className="chat-request-edit-attachment">
                        {attachment.name ?? attachment.file_id}
                        <button
                          type="button"
                          disabled={actionRunning}
                          aria-label={`移除附件 ${attachment.name ?? attachment.file_id}`}
                          onClick={() => setEditAttachments((current) =>
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
                              editAttachments.length + index,
                            ),
                          ),
                        ).then((added) => {
                          setEditAttachments((current) => [...current, ...added]);
                        }).catch((error: unknown) => {
                          setActionError(
                            error instanceof Error ? error.message : String(error),
                          );
                        });
                      }}
                    />
                    <button
                      type="button"
                      className="chat-request-edit-add-attachment"
                      disabled={actionRunning}
                      onClick={() => editAttachmentInputRef.current?.click()}
                    >
                      <span className="codicon codicon-attach" aria-hidden="true" />
                      添加附件
                    </button>
                  </>
                ) : null}
                {!conversation.pending ? (
                  <div className="chat-turn-action-warning">
                    将移除此消息之后的会话上下文，但不会撤销已产生的文件修改。
                  </div>
                ) : null}
                <div className="chat-request-edit-actions">
                  <button
                    type="button"
                    disabled={actionRunning}
                    onClick={() => {
                      setEditing(false);
                      setActionError(null);
                    }}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="primary"
                    disabled={
                      actionRunning
                      || (!editContent.trim() && editAttachments.length === 0)
                    }
                  >
                    {actionRunning
                      ? "正在保存..."
                      : conversation.pending
                        ? "保存"
                        : "编辑并从此处继续"}
                  </button>
                </div>
              </form>
            ) : (
              <>
                {!conversation.pending ? (
                  <button
                    type="button"
                    className="chat-request-edit-button"
                    title="编辑并从此处继续"
                    aria-label="编辑并从此处继续"
                    disabled={sessionBusy}
                    onClick={startEditing}
                  >
                    <span className="codicon codicon-edit" aria-hidden="true" />
                  </button>
                ) : null}
                {userMessage.content ? <div className="chat-user-text">{userMessage.content}</div> : null}
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
            {conversation.pending && userMessage && conversation.pendingKind ? (
              <PendingRequestActions
                kind={conversation.pendingKind}
                disabled={actionRunning}
                onEdit={startEditing}
                onSendImmediately={() => {
                  void executePendingAction(
                    () => onSendPendingImmediately(userMessage.message_id),
                  );
                }}
                onRemove={() => {
                  void executePendingAction(
                    () => onRemovePending(userMessage.message_id),
                  );
                }}
                onChangeKind={(kind) => {
                  void executePendingAction(
                    () => onChangePendingKind(userMessage.message_id, kind),
                  );
                }}
              />
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="chat-assistant-row">
        <div className="chat-assistant-avatar" aria-hidden="true">
          <span className="codicon codicon-copilot" />
        </div>
        <div className="chat-assistant-content">
          {renderGroups.map((group) =>
            group.kind === "work" ? (
              <ThinkingSection
                key={group.id}
                items={group.items}
                active={running && group.items.some((item) => item.active)}
                showRawDetails={showRawDetails}
              />
            ) : (
              <ResponsePart
                key={group.id}
                item={group.item}
                showRawDetails={showRawDetails}
              />
            ),
          )}
          {fallback ? <MarkdownContent value={fallback} /> : null}
          {status ? (
            <div className="chat-working" role="status">
              <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
              <span>{status.title}</span>
              <span className="chat-working-detail">{status.detail}</span>
            </div>
          ) : null}
          {showResponseActions ? (
            <ResponseActionToolbar
              responseText={finalResponseText}
              tokenUsage={tokenUsage}
              canRegenerate={isLastTurn && !sessionBusy}
              onRegenerate={() => setConfirmAction("regenerate")}
            />
          ) : null}
          {failedByJob ? (
            // TODO: 后续为失败轮次重试补齐重试策略、模型切换和参数选择；当前按原输入重试。
            <button
              type="button"
              className="chat-failed-retry-button"
              disabled={actionRunning || sessionBusy}
              onClick={() => setConfirmAction("retry_failed")}
            >
              <span className="codicon codicon-refresh" aria-hidden="true" />
              重试失败轮次
            </button>
          ) : null}
          {confirmAction ? (
            <div className="chat-turn-action-confirmation" role="group" aria-label="确认轮次操作">
              <div className="chat-turn-action-warning">
                将移除此消息之后的会话上下文，但不会撤销已产生的文件修改。
              </div>
              <div className="chat-request-edit-actions">
                <button
                  type="button"
                  disabled={actionRunning}
                  onClick={() => setConfirmAction(null)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="primary"
                  disabled={actionRunning}
                  onClick={() => void executeReplay(confirmAction)}
                >
                  {actionRunning
                    ? "正在执行..."
                    : confirmAction === "regenerate"
                      ? "确认重新生成"
                      : "确认重试"}
                </button>
              </div>
            </div>
          ) : null}
          {actionError ? (
            <div className="chat-inline-error" role="alert">
              <span className="codicon codicon-error" aria-hidden="true" />
              <span>{actionError}</span>
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}
