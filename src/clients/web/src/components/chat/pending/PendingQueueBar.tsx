import { t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import { errorMessage } from "../../../utils/errorMessage";
import {
  DELIVERY_POLICIES,
  DELIVERY_POLICY_LABELS,
} from "../../deliveryPolicyPresentation";

import type { DeliveryPolicy } from "../../../types/backend";
import type { ConversationView } from "../../../types/frontend";
import { queuedThreadMessages } from "../../../state/threadStatus";
import { ComposerDiagnosticsContext } from "../../composer/ComposerDiagnosticsContext";

const PendingQueueItem = React.memo(function PendingQueueItem({
  conversation,
  onRemove,
  onChangePolicy,
  reorder,
}: {
  reorder?: { disabled: boolean; start: () => void; end: () => void; drop: () => void; move: (direction: number) => void };
  conversation: ConversationView;
  onRemove: (messageId: string) => Promise<void>;
  onChangePolicy: (
    messageId: string,
    policy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => Promise<void>;
}): React.ReactNode {
  const userMessage = conversation.userMessage;
  const [directionOpen, setDirectionOpen] = React.useState(false);
  const [actionRunning, setActionRunning] = React.useState(false);
  const [error, setError] = React.useState(false);
  const report = React.useContext(ComposerDiagnosticsContext);

  React.useEffect(() => {
    setDirectionOpen(false);
    setError(false);
  }, [userMessage?.content, userMessage?.message_id]);

  if (!userMessage || !conversation.deliveryPolicy) {
    return null;
  }

  const runAction = async (action: () => Promise<void>) => {
    if (actionRunning) return;
    setActionRunning(true);
    setError(false);
    try {
      await action();
    } catch (actionError) {
      setError(true);
      report(`message_id=${userMessage.message_id}: ${errorMessage(actionError)}`);
    } finally {
      setActionRunning(false);
    }
  };

  return (
    <div
      className="chat-pending-composer-item"
      role="listitem"
      data-message-id={userMessage.message_id}
      data-turn-id={conversation.turnId}
      aria-label={t("chat.pending.item", conversation.enqueueSequence ?? conversation.pendingPosition ?? "", userMessage.content)}
      title={conversation.waitingReason ?? undefined}
      onDragOver={event => { if (reorder && !reorder.disabled) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } }}
      onDrop={event => { event.preventDefault(); event.stopPropagation(); reorder?.drop(); }}
    >
      <div className="chat-pending-composer-item-row">
        {reorder ? <button type="button" className="chat-pending-icon-button chat-pending-drag-handle"
          draggable={!reorder.disabled} disabled={reorder.disabled} title={t("chat.pending.reorder")} aria-label={t("chat.pending.reorder")}
          onDragStart={event => { event.stopPropagation(); event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("application/x-boxteam-pending-message", userMessage.message_id); reorder.start(); }}
          onDragEnd={reorder.end} onKeyDown={event => {
            if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
              event.preventDefault(); event.stopPropagation(); reorder.move(event.key === "ArrowUp" ? -1 : 1);
            }
          }}><span className="codicon codicon-gripper" aria-hidden="true" /></button> : null}
        <span className="chat-pending-composer-item-content">
          {userMessage.content || t("chat.pending.attachment")}
        </span>
        <span className={`chat-pending-composer-item-status${error ? " needs-attention" : ""}`}
          role={error ? "alert" : undefined} title={error ? t("composer.operationFailed") : conversation.waitingReason ?? t("chat.pending.queued")}>
          {error ? t("composer.operationFailed") : t("chat.pending.queued")}
        </span>
        <div className="chat-pending-composer-item-actions">
          <button
            type="button"
            className="chat-pending-direction-button"
            disabled={actionRunning}
            aria-label={DELIVERY_POLICY_LABELS[conversation.deliveryPolicy]}
            title={DELIVERY_POLICY_LABELS[conversation.deliveryPolicy]}
            aria-expanded={directionOpen}
            aria-haspopup="menu"
            onClick={() => setDirectionOpen((open) => !open)}
          >
            <span className="codicon codicon-arrow-right" aria-hidden="true" />
            <span className="codicon codicon-chevron-down" aria-hidden="true" />
          </button>
          <button
            type="button"
            className="chat-pending-icon-button"
            disabled={actionRunning}
            title={t("chat.pending.withdraw")}
            aria-label={t("chat.pending.withdraw")}
            onClick={() => void runAction(() => onRemove(userMessage.message_id))}
          >
            <span className="codicon codicon-trash" aria-hidden="true" />
          </button>
        </div>
      </div>
      {directionOpen ? (
        <div className="chat-pending-direction-menu" role="menu" aria-label={t("chat.pending.directionMenu")}>
          {DELIVERY_POLICIES.map((policy) => (
            <button
              key={policy}
              type="button"
              role="menuitemradio"
              aria-checked={conversation.deliveryPolicy === policy}
              disabled={actionRunning || conversation.deliveryPolicy === policy}
              onClick={() => {
                setDirectionOpen(false);
                void runAction(() => onChangePolicy(
                  userMessage.message_id,
                  policy,
                  conversation.queueSnapshotVersion,
                ));
              }}
            >
              <span>{DELIVERY_POLICY_LABELS[policy]}</span>
              {conversation.deliveryPolicy === policy ? <span aria-hidden="true">✓</span> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
});

export default function PendingQueueBar({
  conversations,
  onClear,
  onRemove,
  onChangePolicy,
  onReorder,
}: {
  onReorder?: (messageIds: string[], expectedThreadRevision: number) => Promise<void>;
  conversations: ConversationView[];
  onClear: () => Promise<void>;
  onRemove: (messageId: string) => Promise<void>;
  onChangePolicy: (
    messageId: string,
    policy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => Promise<void>;
}): React.ReactNode {
  const queued = React.useMemo(
    () => queuedThreadMessages(conversations),
    [conversations],
  );
  const [optimistic, setOptimistic] = React.useState<{ ids: string[]; revision: number }>();
  const dragging = React.useRef<string>();
  const revision = queued[0]?.queueThreadRevision;
  const movable = queued.filter(item => item.queueReorderable);
  const ids = movable.map(item => item.userMessage!.message_id);
  const applicable = optimistic && revision === optimistic.revision && ids.length === optimistic.ids.length
    && ids.every(id => optimistic.ids.includes(id));
  let slot = 0;
  const ordered = applicable ? queued.map(item => {
    if (!item.queueReorderable) return item;
    const messageId = optimistic.ids[slot++];
    return movable.find(value => value.userMessage!.message_id === messageId)!;
  }) : queued;
  const reorder = (from: string, to: number) => {
    if (!onReorder || revision === undefined || applicable || to < 0 || to >= ids.length) return;
    const next = [...ids], old = next.indexOf(from);
    if (old < 0 || old === to) return;
    next.splice(old, 1); next.splice(to, 0, from);
    const intent = { ids: next, revision };
    setOptimistic(intent);
    void onReorder(next, revision).then(() => setOptimistic(current => current === intent ? undefined : current))
      .catch(cause => report(errorMessage(cause)));
    // 对账也失败时保留未知结果，等待原 SSE 的权威更新；不盲回滚或显示排序错误行。
  };
  const [clearing, setClearing] = React.useState(false);
  const [error, setError] = React.useState(false);
  const report = React.useContext(ComposerDiagnosticsContext);

  if (queued.length === 0) {
    return null;
  }

  const clear = async () => {
    if (clearing) return;
    setClearing(true);
    setError(false);
    try {
      await onClear();
    } catch (clearError) {
      setError(true);
      report(`${t("chat.pending.withdrawAll")}: ${errorMessage(clearError)}`);
    } finally {
      setClearing(false);
    }
  };

  return (
    <div
      className="chat-pending-composer-bar"
      aria-label={t("chat.pending.count", queued.length)}
      data-queue-size={queued.length}
    >
      <div className="chat-pending-composer-items" role="list">
        {ordered.map((conversation) => (
          <PendingQueueItem
            key={conversation.conversationId}
            conversation={conversation}
            reorder={onReorder && conversation.queueReorderable && ids.length > 1 ? {
              disabled: Boolean(applicable) || revision === undefined,
              start: () => { dragging.current = conversation.userMessage!.message_id; },
              end: () => { dragging.current = undefined; },
              drop: () => { if (dragging.current) reorder(dragging.current, ids.indexOf(conversation.userMessage!.message_id)); dragging.current = undefined; },
              move: direction => reorder(conversation.userMessage!.message_id, ids.indexOf(conversation.userMessage!.message_id) + direction),
            } : undefined}
            onRemove={onRemove}
            onChangePolicy={onChangePolicy}
          />
        ))}
      </div>
      <button
        type="button"
        className="chat-pending-composer-clear"
        disabled={clearing}
        title={error ? t("composer.operationFailed") : t("chat.pending.withdrawAll")}
        aria-label={t("chat.pending.withdrawAll")}
        onClick={() => void clear()}
      >
        <span className={`codicon ${error ? "codicon-warning" : "codicon-trash"}`} aria-hidden="true" />
      </button>
    </div>
  );
}
