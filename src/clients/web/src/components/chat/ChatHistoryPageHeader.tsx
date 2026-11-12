import { t } from "../../../../shared/core/i18n/index.js";
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
        <span role="status">{t("chat.history.migratingOlder")}</span>
      ) : hasOlderMessages ? (
        <span role="status">
          {loadingOlderMessages ? t("chat.history.loadingOlder") : t("chat.history.scrollOlder")}
        </span>
      ) : (
        <span>{t("chat.history.start")}</span>
      )}
      {error ? (
        <div className="chat-history-page-error" role="alert" title={error}>
          <span className="chat-history-page-error-message">{error}</span>
          <button type="button" onClick={onRetry}>{t("chat.retry")}</button>
        </div>
      ) : null}
    </div>
  );
}
