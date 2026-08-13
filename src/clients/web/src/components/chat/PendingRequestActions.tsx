import React from "react";

import type { PendingRequestKind } from "../../types/backend";

export default function PendingRequestActions({
  kind,
  disabled,
  onEdit,
  onSendImmediately,
  onRemove,
  onChangeKind,
}: {
  kind: PendingRequestKind;
  disabled: boolean;
  onEdit: () => void;
  onSendImmediately: () => void;
  onRemove: () => void;
  onChangeKind: (kind: PendingRequestKind) => void;
}): React.ReactNode {
  return (
    <div
      className="chat-pending-actions"
      role="toolbar"
      aria-label="待处理消息操作"
    >
      <span className="chat-pending-kind">
        {kind === "steering" ? "引导" : "已排队"}
      </span>
      <button type="button" disabled={disabled} onClick={onEdit} title="编辑">
        <span className="codicon codicon-edit" aria-hidden="true" />
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={onSendImmediately}
        title="立即发送"
      >
        <span className="codicon codicon-debug-continue" aria-hidden="true" />
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChangeKind(kind === "steering" ? "queued" : "steering")}
        title={kind === "steering" ? "改为当前请求完成后发送" : "改为引导消息"}
      >
        <span
          className={`codicon codicon-${kind === "steering" ? "list-ordered" : "git-pull-request-go-to-changes"}`}
          aria-hidden="true"
        />
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={onRemove}
        title="从队列撤回"
      >
        <span className="codicon codicon-close" aria-hidden="true" />
      </button>
    </div>
  );
}
