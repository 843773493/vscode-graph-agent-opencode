import React from "react";
import type { TurnProjectionState } from "../../state/session/turnTimeline";

export default function ChatHistoryPageHeader({
  projectionState,
  hasOlderMessages,
  loadingOlderMessages,
  error,
  onRetry,
}: {
  projectionState: TurnProjectionState;
  hasOlderMessages: boolean;
  loadingOlderMessages: boolean;
  error: string | null;
  onRetry: () => void;
}): React.ReactNode {
  return (
    <div className="chat-history-page-header">
      {projectionState === "partial" ? (
        <span role="status">旧 Turn 正在迁移，完成后可继续向上加载</span>
      ) : hasOlderMessages ? (
        <span role="status">
          {loadingOlderMessages ? "正在加载更早消息…" : "继续向上滚动加载更早消息"}
        </span>
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
