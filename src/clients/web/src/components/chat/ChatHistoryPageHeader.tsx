import { t } from "../../../../shared/core/i18n/index.js";
import React from "react";

export default function ChatHistoryPageHeader({
  loadingOlderMessages,
  error,
  onRetry,
}: {
  loadingOlderMessages: boolean;
  error: string | null;
  onRetry: () => void;
}): React.ReactNode {
  return (
    <div className="chat-history-page-header" aria-busy={loadingOlderMessages}>
      {loadingOlderMessages && !error ? <span className="chat-history-page-progress codicon codicon-loading codicon-modifier-spin" role="status" aria-label={t("chat.history.loadingOlder")} /> : null}
      {error ? (
        <div className="chat-history-page-error" role="alert" title={error}>
          <span className="chat-history-page-error-message">{error}</span>
          <button type="button" onClick={onRetry}>{t("chat.retry")}</button>
        </div>
      ) : null}
    </div>
  );
}
