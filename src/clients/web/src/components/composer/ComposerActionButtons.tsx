import React from "react";

export default function ComposerActionButtons({
  hasContent,
  showInterrupt,
  onClear,
  onInterrupt,
  onSend,
  onAlternate,
  onToggleDefault,
  defaultPendingKind,
}: {
  hasContent: boolean;
  showInterrupt: boolean;
  onClear: () => void;
  onInterrupt: () => void;
  onSend: () => void;
  onAlternate: () => void;
  onToggleDefault: () => void;
  defaultPendingKind: "steering" | "queued";
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
          title={
            defaultPendingKind === "steering"
              ? "改为排队，在当前请求完整结束后发送"
              : "改为引导，在下一个安全边界发送"
          }
          aria-label="使用另一种待处理消息发送方式"
        >
          <span
            className={`codicon codicon-${defaultPendingKind === "steering" ? "list-ordered" : "git-pull-request-go-to-changes"}`}
            aria-hidden="true"
          />
        </button>
      )}
      {showInterrupt && (
        <button
          type="button"
          className="composer-icon-button hover-only"
          onClick={onToggleDefault}
          title={`默认发送方式：${defaultPendingKind === "steering" ? "引导" : "排队"}，点击切换`}
          aria-label="切换运行中消息的默认发送方式"
        >
          <span className="codicon codicon-settings-gear" aria-hidden="true" />
        </button>
      )}
    </>
  );
}
