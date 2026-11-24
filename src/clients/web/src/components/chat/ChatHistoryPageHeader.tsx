import { t } from "../../../../shared/core/i18n/index.js";
import React from "react";
import { failureSummary } from "../../../../shared/core/failurePresentation";
import { copyItemAction, useChatItemMenu } from "./itemContextMenu";

export default function ChatHistoryPageHeader({
  loadingOlderMessages,
  error,
  onRetry,
}: {
  loadingOlderMessages: boolean;
  error: string | null;
  onRetry: () => void;
}): React.ReactNode {
  const menu = useChatItemMenu(() => error ? [copyItemAction(error)] : [], Boolean(error));
  return (
    <div className="chat-history-page-header" aria-busy={loadingOlderMessages}>
      {loadingOlderMessages && !error ? <span className="chat-history-page-progress codicon codicon-loading codicon-modifier-spin" role="status" aria-label={t("chat.history.loadingOlder")} /> : null}
      {error ? (
        <div className="chat-history-page-error" role="alert" {...menu}>
          <span className="chat-history-page-error-message">{failureSummary(error)}</span>
          <button type="button" onClick={onRetry}>{t("chat.retry")}</button>
        </div>
      ) : null}
    </div>
  );
}
