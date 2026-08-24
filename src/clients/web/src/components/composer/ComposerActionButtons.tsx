import React from "react";

const DELIVERY_POLICY_LABELS = {
  after_turn: "本轮结束后投递",
  after_tool_result: "工具结果后投递",
  after_interrupt: "中断边界后投递",
} as const;

export default function ComposerActionButtons({
  hasContent,
  showInterrupt,
  onClear,
  onInterrupt,
  onSend,
  onAlternate,
  onToggleDefault,
  defaultDeliveryPolicy,
}: {
  hasContent: boolean;
  showInterrupt: boolean;
  onClear: () => void;
  onInterrupt: () => void;
  onSend: () => void;
  onAlternate: () => void;
  onToggleDefault: () => void;
  defaultDeliveryPolicy: "after_turn" | "after_tool_result" | "after_interrupt";
}): React.ReactNode {
  return (
    <>
      {hasContent && (
        <button
          id="clearInputButton"
          type="button"
          title="清空输入"
          className="composer-icon-button hover-only"
          onClick={onClear}
        >
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <path
              d="M3 3l10 10M13 3L3 13"
              stroke="currentColor"
              strokeWidth="1.2"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          </svg>
        </button>
      )}
      {showInterrupt && (
        <button
          id="interruptButton"
          type="button"
          className="composer-icon-button interrupt-button"
          onClick={onInterrupt}
          title="中断生成"
        >
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <rect
              x="3"
              y="3"
              width="10"
              height="10"
              rx="2"
              fill="currentColor"
            />
          </svg>
        </button>
      )}
      <button
        id="sendButton"
        type="button"
        className="send-button"
        disabled={!hasContent}
        onClick={onSend}
        title={hasContent ? "发送消息" : "输入消息以启用发送"}
        aria-label={hasContent ? "发送消息" : "输入消息以启用发送"}
      >
        <svg viewBox="0 0 16 16" width="12" height="12">
          <path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z" />
        </svg>
      </button>
      {showInterrupt && (
        <button
          type="button"
          className="composer-icon-button queue-message-button"
          disabled={!hasContent}
          onClick={onAlternate}
          title="使用下一种投递边界发送"
          aria-label="使用下一种投递边界发送"
        >
          <span
            className="codicon codicon-list-ordered"
            aria-hidden="true"
          />
        </button>
      )}
      {showInterrupt && (
        <button
          type="button"
          className="composer-icon-button hover-only"
          onClick={onToggleDefault}
          title={`新消息默认：${DELIVERY_POLICY_LABELS[defaultDeliveryPolicy]}；点击切换`}
          aria-label={`切换新消息默认投递策略（当前为${DELIVERY_POLICY_LABELS[defaultDeliveryPolicy]}）`}
        >
          <span className="codicon codicon-settings-gear" aria-hidden="true" />
        </button>
      )}
    </>
  );
}
