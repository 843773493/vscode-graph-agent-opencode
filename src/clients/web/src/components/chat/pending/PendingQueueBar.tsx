import React from "react";
import { errorMessage } from "../../../utils/errorMessage";
import {
  DELIVERY_POLICIES,
  DELIVERY_POLICY_LABELS,
} from "../../deliveryPolicyPresentation";

import type { DeliveryPolicy } from "../../../types/backend";
import type { ConversationView } from "../../../types/frontend";

function PendingQueueItem({
  conversation,
  onRemove,
  onChangePolicy,
}: {
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
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    setDirectionOpen(false);
    setError(null);
  }, [userMessage?.content, userMessage?.message_id]);

  if (!userMessage || !conversation.deliveryPolicy) {
    return null;
  }

  const runAction = async (action: () => Promise<void>) => {
    if (actionRunning) return;
    setActionRunning(true);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(errorMessage(actionError));
    } finally {
      setActionRunning(false);
    }
  };

  return (
    <div
      className="chat-pending-composer-item"
      role="listitem"
      aria-label={`排队消息 ${conversation.enqueueSequence ?? conversation.pendingPosition ?? ""}：${userMessage.content}`}
      title={conversation.waitingReason ?? undefined}
    >
      <div className="chat-pending-composer-item-row">
        <span className="chat-pending-composer-item-icon" aria-hidden="true">
          <span className="codicon codicon-arrow-right" />
        </span>
        <span className="chat-pending-composer-item-content">
          {userMessage.content || "（附件消息）"}
        </span>
        <div className="chat-pending-composer-item-actions">
          <button
            type="button"
            className="chat-pending-direction-button"
            disabled={actionRunning}
            aria-expanded={directionOpen}
            aria-haspopup="menu"
            onClick={() => setDirectionOpen((open) => !open)}
          >
            <span aria-hidden="true">↪</span>
            调整方向
          </button>
          <button
            type="button"
            className="chat-pending-icon-button"
            disabled={actionRunning}
            title="撤回排队消息"
            aria-label="撤回排队消息"
            onClick={() => void runAction(() => onRemove(userMessage.message_id))}
          >
            <span className="codicon codicon-trash" aria-hidden="true" />
          </button>
        </div>
      </div>
      {directionOpen ? (
        <div className="chat-pending-direction-menu" role="menu" aria-label="调整投递方向">
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
      {error ? <div className="chat-pending-composer-error" role="alert">{error}</div> : null}
    </div>
  );
}

export default function PendingQueueBar({
  conversations,
  onClear,
  onRemove,
  onChangePolicy,
}: {
  conversations: ConversationView[];
  onClear: () => Promise<void>;
  onRemove: (messageId: string) => Promise<void>;
  onChangePolicy: (
    messageId: string,
    policy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => Promise<void>;
}): React.ReactNode {
  const queued = conversations
    .filter((conversation) =>
      conversation.pending
      && !conversation.activeJobOverlay
      && conversation.userMessage
      && conversation.deliveryPolicy,
    )
    .sort((left, right) =>
      (left.enqueueSequence ?? Number.MAX_SAFE_INTEGER)
      - (right.enqueueSequence ?? Number.MAX_SAFE_INTEGER),
    );
  const [clearing, setClearing] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  if (queued.length === 0) {
    return null;
  }

  const clear = async () => {
    if (clearing) return;
    setClearing(true);
    setError(null);
    try {
      await onClear();
    } catch (clearError) {
      setError(errorMessage(clearError));
    } finally {
      setClearing(false);
    }
  };

  return (
    <div
      className="chat-pending-composer-bar"
      aria-label={`待处理消息队列，共 ${queued.length} 条`}
      data-queue-size={queued.length}
    >
      <div className="chat-pending-composer-items" role="list">
        {queued.map((conversation) => (
          <PendingQueueItem
            key={conversation.conversationId}
            conversation={conversation}
            onRemove={onRemove}
            onChangePolicy={onChangePolicy}
          />
        ))}
      </div>
      <button
        type="button"
        className="chat-pending-composer-clear"
        disabled={clearing}
        title="全部撤回"
        aria-label="全部撤回"
        onClick={() => void clear()}
      >
        <span className="codicon codicon-trash" aria-hidden="true" />
      </button>
      {error ? <span className="chat-pending-composer-error" role="alert">{error}</span> : null}
    </div>
  );
}
