import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
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
          <div className="chat-stream-empty-title">{t("chat.history.failed")}</div>
          <div className="chat-stream-empty-detail" role="alert">{historyError}</div>
          <button
            type="button"
            className="chat-stream-empty-action"
            onClick={onRetryHistory}
          >
            {t("chat.history.retry")}
          </button>
        </>
      ) : historyLoading ? (
        <>
          <div className="chat-stream-empty-title">{t("chat.history.latest")}</div>
          <div className="chat-stream-empty-detail">
            {t("chat.history.usable")}
          </div>
        </>
      ) : projectionState === "partial" ? (
        <>
          <div className="chat-stream-empty-title">{t("chat.history.migrating")}</div>
          <div className="chat-stream-empty-detail">
            {t("chat.history.migratingDetail")}
          </div>
        </>
      ) : (
        <>
          <div className="chat-stream-empty-title">{t("chat.history.empty")}</div>
          <div className="chat-stream-empty-detail">
            {t("chat.history.emptyDetail")}
          </div>
        </>
      )}
      {sessionChangeSummary && sessionChangeSummary.files > 0 ? (
        <button
          type="button"
          className="chat-stream-empty-action"
          onClick={onOpenChanges}
        >
          {t("chat.history.changes", formatNumber(sessionChangeSummary.files))}
        </button>
      ) : sessionChangesLoading ? (
        <div className="chat-stream-empty-detail">{t("chat.history.changesLoading")}</div>
      ) : null}
    </div>
  );
}
