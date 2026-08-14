import React from "react";
import type { TurnProjectionState } from "../../state/session/turnTimeline";
import type { SessionChangesSummary } from "../../types/backend";

export default function ChatHistoryEmptyState({
  historyError,
  historyLoading,
  projectionState,
  onRetryHistory,
  sessionChangeSummary,
  sessionChangesLoading,
  onOpenChanges,
}: {
  historyError: string | null;
  historyLoading: boolean;
  projectionState: TurnProjectionState;
  onRetryHistory: () => void;
  sessionChangeSummary?: SessionChangesSummary | null;
  sessionChangesLoading?: boolean;
  onOpenChanges?: () => void;
}): React.ReactNode {
  return (
    <div className="chat-stream-empty-history" role="status">
      {historyError ? (
        <>
          <div className="chat-stream-empty-title">无法加载会话历史</div>
          <div className="chat-stream-empty-detail" role="alert">{historyError}</div>
          <button
            type="button"
            className="chat-stream-empty-action"
            onClick={onRetryHistory}
          >
            重试加载
          </button>
        </>
      ) : historyLoading ? (
        <>
          <div className="chat-stream-empty-title">正在加载最新 Turn</div>
          <div className="chat-stream-empty-detail">
            Composer 已可使用，历史内容将在这里原位显示。
          </div>
        </>
      ) : projectionState === "partial" ? (
        <>
          <div className="chat-stream-empty-title">旧 Turn 正在迁移</div>
          <div className="chat-stream-empty-detail">
            Composer 已可使用，迁移完成后会自动显示历史内容。
          </div>
        </>
      ) : (
        <>
          <div className="chat-stream-empty-title">该会话暂无历史消息</div>
          <div className="chat-stream-empty-detail">
            在下方输入任务，Assistant 的回复会显示在这里。
          </div>
        </>
      )}
      {sessionChangeSummary && sessionChangeSummary.files > 0 ? (
        <button
          type="button"
          className="chat-stream-empty-action"
          onClick={onOpenChanges}
        >
          本会话有 {sessionChangeSummary.files} 个文件变更待审查
        </button>
      ) : sessionChangesLoading ? (
        <div className="chat-stream-empty-detail">正在检查会话文件变更...</div>
      ) : null}
    </div>
  );
}
