import React from "react";

import type { DeliveryPolicy } from "../../types/backend";
import {
  DELIVERY_POLICIES,
  DELIVERY_POLICY_LABELS,
} from "../deliveryPolicyPresentation";

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
      <span className="chat-pending-kind">{DELIVERY_POLICY_LABELS[deliveryPolicy]}</span>
      {DELIVERY_POLICIES.map((policy) => (
        <button
          key={policy}
          type="button"
          disabled={disabled || policy === deliveryPolicy}
          aria-pressed={policy === deliveryPolicy}
          title={DELIVERY_POLICY_LABELS[policy]}
          aria-label={DELIVERY_POLICY_LABELS[policy]}
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
