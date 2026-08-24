import React from "react";

import type { DeliveryPolicy } from "../../types/backend";

const POLICY_LABELS: Record<DeliveryPolicy, string> = {
  after_turn: "本轮结束后投递",
  after_tool_result: "工具结果后投递",
  after_interrupt: "中断边界后投递",
};

export default function PendingRequestActions({
  deliveryPolicy,
  disabled,
  onEdit,
  onRemove,
  onChangePolicy,
}: {
  deliveryPolicy: DeliveryPolicy;
  disabled: boolean;
  onEdit: () => void;
  onRemove: () => void;
  onChangePolicy: (policy: DeliveryPolicy) => void;
}): React.ReactNode {
  return (
    <div
      className="chat-pending-actions"
      role="toolbar"
      aria-label="待处理消息投递策略"
    >
      <span className="chat-pending-kind">{POLICY_LABELS[deliveryPolicy]}</span>
      {(Object.keys(POLICY_LABELS) as DeliveryPolicy[]).map((policy) => (
        <button
          key={policy}
          type="button"
          disabled={disabled || policy === deliveryPolicy}
          aria-pressed={policy === deliveryPolicy}
          title={POLICY_LABELS[policy]}
          aria-label={POLICY_LABELS[policy]}
          onClick={() => onChangePolicy(policy)}
        >
          {policy === deliveryPolicy ? "✓" : "·"}
        </button>
      ))}
      <button
        type="button"
        disabled={disabled}
        onClick={onEdit}
        title="编辑待处理消息"
        aria-label="编辑待处理消息"
      >
        <span className="codicon codicon-edit" aria-hidden="true" />
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={onRemove}
        title="从 FIFO 队列撤回"
        aria-label="从 FIFO 队列撤回"
      >
        <span className="codicon codicon-close" aria-hidden="true" />
      </button>
    </div>
  );
}
