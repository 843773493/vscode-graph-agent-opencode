import React from "react";
import type { TurnProjectionState } from "../../state/session/turnTimeline";

export default function ChatHistoryPageHeader({
  projectionState,
  hasOlderMessages,
  loadingOlderMessages,
  error,
  onLoadOlder,
  onRetry,
}: {
  projectionState: TurnProjectionState;
  hasOlderMessages: boolean;
  loadingOlderMessages: boolean;
  error: string | null;
  onLoadOlder: () => void;
  onRetry: () => void;
}): React.ReactNode {
  return (
    <div className="chat-history-page-header">
      {projectionState === "partial" ? (
        <span role="status">旧 Turn 正在迁移，完成后可继续向上加载</span>
      ) : hasOlderMessages ? (
        <button
          type="button"
          disabled={loadingOlderMessages}
          onClick={onLoadOlder}
        >
          {loadingOlderMessages ? "正在加载更早消息…" : "加载更早消息"}
        </button>
      ) : (
        <span>已到达会话起点</span>
      )}
      {error ? (
        <>
          <span role="alert">{error}</span>
          <button type="button" onClick={onRetry}>重试</button>
        </>
      ) : null}
    </div>
  );
}
